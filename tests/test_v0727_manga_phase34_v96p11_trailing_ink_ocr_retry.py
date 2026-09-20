from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _image(*, suffix_ink: bool = True) -> Image.Image:
    image = Image.new("RGB", (220, 420), "white")
    draw = ImageDraw.Draw(image)
    glyphs = [(40, 66), (72, 98), (104, 130), (136, 162)]
    if suffix_ink:
        glyphs += [(168, 194), (200, 226)]
    for top, bottom in glyphs:
        draw.rectangle((90, top, 116, bottom), fill="black")
    return image


def _item() -> dict[str, object]:
    top, bottom = 38, 164
    return {
        "text": "どんな理",
        "raw_text": "どんな理",
        "orientation": "vertical",
        "x": 88 / 220,
        "y": 1.0 - bottom / 420,
        "width": 30 / 220,
        "height": (bottom - top) / 420,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "trailing_ink_geometry": True,
        "provenance": {
            "component_count": 2,
            "component_coverage": 1.0,
            "single_merged_component": True,
            "trailing_ink_geometry": {
                "old_bottom_px": 130,
                "new_bottom_px": bottom,
                "extension_px": bottom - 130,
                "scan_x_px": [86, 120],
            },
        },
        "hypotheses": [
            {"id": "manga-ocr-trailing-ink", "text": "どんな理", "source": "manga-ocr", "selected": True}
        ],
        "selected_hypothesis_id": "manga-ocr-trailing-ink",
    }


def _model(*, square: str = "どんな理", wide: str = "どんな理由が", wide_square: str | None = None):
    def recognize(crop: Image.Image) -> str:
        if crop.height >= 190:
            if crop.width == crop.height and wide_square is not None:
                return wide_square
            return wide
        if crop.width == crop.height:
            return square
        return "どんな理"

    return recognize


def test_v96_trailing_retry_recovers_only_ink_backed_prefix_extension() -> None:
    retry = getattr(worker, "_recover_vertical_trailing_ocr_retry", None)
    assert retry is not None
    image = _image()
    try:
        repaired = retry(_model(), image, _item())
    finally:
        image.close()

    assert repaired["text"] == "どんな理由が"
    assert repaired["raw_text"] == "どんな理由が"
    assert repaired["recognizer_retry"] == "vertical-trailing-ink-ocr-v2"
    assert repaired["selected_hypothesis_id"] == "manga-ocr-trailing-ink-ocr-v2"
    info = repaired["provenance"]["trailing_ink_ocr_retry"]
    assert info["added_text"] == "由が"
    assert info["direct_core"] == info["square_core"] == "どんな理"
    assert info["new_bottom_px"] > info["old_bottom_px"]
    assert float(repaired["y"]) < float(_item()["y"])


def test_v96_trailing_retry_rejects_hallucinated_suffix_without_new_ink() -> None:
    retry = getattr(worker, "_recover_vertical_trailing_ocr_retry", None)
    assert retry is not None
    image = _image(suffix_ink=False)
    item = _item()
    try:
        repaired = retry(_model(), image, item)
    finally:
        image.close()
    assert repaired == item


def test_v96_trailing_retry_rejects_suffix_whose_first_slot_skips_nearby_ink() -> None:
    retry = getattr(worker, "_recover_vertical_trailing_ocr_retry", None)
    assert retry is not None
    image = _image()
    item = _item()
    try:
        repaired = retry(_model(wide="どんな理由"), image, item)
    finally:
        image.close()
    assert repaired == item


def test_v96_trailing_retry_requires_direct_square_core_consensus() -> None:
    retry = getattr(worker, "_recover_vertical_trailing_ocr_retry", None)
    assert retry is not None
    image = _image()
    item = _item()
    try:
        repaired = retry(_model(square="どんな里"), image, item)
    finally:
        image.close()
    assert repaired == item


def test_v96_trailing_retry_rejects_more_than_three_added_japanese_glyphs() -> None:
    retry = getattr(worker, "_recover_vertical_trailing_ocr_retry", None)
    assert retry is not None
    image = _image()
    item = _item()
    try:
        repaired = retry(_model(wide="どんな理由がまだ"), image, item)
    finally:
        image.close()
    assert repaired == item


def test_v96_pipeline_and_cache_markers_are_current() -> None:
    from pudge import manga

    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    with open(manga.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "-regions-v96p27.json" in source


def test_v961_fallback_rebuilds_intermediate_tail_when_first_single_read_misses() -> None:
    image = _image()
    item = _item()
    item["text"] = "どん"
    item["raw_text"] = "どん"
    item["y"] = 1.0 - 102 / 420
    item["height"] = (102 - 38) / 420
    item["trailing_ink_geometry"] = False
    item["provenance"] = {
        "component_count": 2,
        "component_coverage": 1.0,
        "single_merged_component": True,
        "detector_bbox_px": [88, 38, 118, 102],
    }

    calls = {"mid_direct": 0}

    def recognize(crop: Image.Image) -> str:
        # Existing +48px trailing geometry: first single direct misses, then
        # fallback direct+square agree on the intermediate prefix.
        if crop.height <= 130:
            if crop.width != crop.height:
                calls["mid_direct"] += 1
                if calls["mid_direct"] == 1:
                    return "どん"
            return "どんな理"
        # Detector-anchored final probe.
        return "どんな理由が"

    try:
        repaired = worker._recover_vertical_trailing_context(recognize, image, item)
    finally:
        image.close()

    assert repaired["text"] == "どんな理由が"
    assert repaired["recognizer_retry"] == "vertical-trailing-ink-ocr-v2"
    assert repaired["provenance"]["trailing_ink_ocr_retry"]["intermediate_core"] == "どんな理"


def test_v961_final_wide_probe_requires_direct_square_consensus() -> None:
    retry = getattr(worker, "_recover_vertical_trailing_ocr_retry", None)
    assert retry is not None
    image = _image()
    item = _item()
    try:
        repaired = retry(_model(wide="どんな理由が", wide_square="どんな理由は"), image, item)
    finally:
        image.close()
    assert repaired == item


def test_v962_full_detector_consensus_can_bridge_failed_intermediate_ocr() -> None:
    image = _image()
    item = _item()
    item["text"] = "どん"
    item["raw_text"] = "どん"
    item["y"] = 1.0 - 102 / 420
    item["height"] = (102 - 38) / 420
    item["trailing_ink_geometry"] = False
    item["provenance"] = {
        "component_count": 2,
        "component_coverage": 1.0,
        "single_merged_component": True,
        "detector_bbox_px": [88, 38, 118, 102],
    }

    def recognize(crop: Image.Image) -> str:
        # Real p36 failure mode from the v96p11 installer: every OCR attempt on
        # the first trailing stage can stay at the original two-glyph surface.
        # The wider detector-anchored crop is stable and sees the full lane.
        if crop.height <= 130:
            return "どん"
        return "どんな理由が"

    try:
        repaired = worker._recover_vertical_trailing_context(recognize, image, item)
    finally:
        image.close()

    assert repaired["text"] == "どんな理由が"
    assert repaired["recognizer_retry"] == "vertical-trailing-detector-consensus-v1"
    info = repaired["provenance"]["trailing_detector_consensus"]
    assert info["stage1_added_text"] == "な理"
    assert info["stage2_added_text"] == "由が"
    assert info["stage1_added_japanese_glyphs"] == 2
    assert info["stage2_added_japanese_glyphs"] == 2


def test_v963_detector_anchor_fallback_ignores_mutated_current_geometry() -> None:
    image = _image()
    item = _item()
    item["text"] = "どん"
    item["raw_text"] = "どん"
    # Model the real fresh-pipeline failure: later geometry has moved away from
    # the detector lane, while detector_bbox_px still points at the observed ink.
    item["x"] = 150 / 220
    item["y"] = 1.0 - 108 / 420
    item["width"] = 12 / 220
    item["height"] = 34 / 420
    item["trailing_ink_geometry"] = False
    item["provenance"] = {
        "component_count": 2,
        "component_coverage": 1.0,
        "single_merged_component": True,
        "detector_bbox_px": [88, 38, 118, 102],
    }

    def recognize(crop: Image.Image) -> str:
        if crop.height >= 150:
            return "どんな理由が"
        return "どん"

    try:
        repaired = worker._recover_vertical_edge_context(recognize, image, item)
    finally:
        image.close()

    assert repaired["text"] == "どんな理由が"
    assert repaired["recognizer_retry"] == "vertical-trailing-detector-anchor-consensus-v1"
    info = repaired["provenance"]["trailing_detector_anchor_consensus"]
    assert info["stage1_added_text"] == "な理"
    assert info["stage2_added_text"] == "由が"


def test_v964_late_short_wide_donor_runs_bounded_trailing_recovery() -> None:
    recover = getattr(worker, "_recover_short_wide_vertical_donor_trailing_context", None)
    assert recover is not None
    image = _image()
    donor = _item()
    donor["text"] = "どん"
    donor["raw_text"] = "どん"
    donor["y"] = 1.0 - 102 / 420
    donor["height"] = (102 - 38) / 420
    donor["detector"] = "wide-vertical-text-donor-v1"
    donor["geometry_source"] = "wide-vertical-layout-donor-v1"
    donor["geometry_status"] = "approximate"
    donor["trailing_ink_geometry"] = False
    donor["provenance"] = {
        "component_count": 2,
        "component_coverage": 1.0,
        "single_merged_component": True,
        "detector_bbox_px": [88, 38, 118, 102],
        "cluster_context_donor": True,
        "wide_vertical_text_donor": True,
    }

    def recognize(crop: Image.Image) -> str:
        if crop.height >= 150:
            return "どんな理由が"
        if crop.height >= 115:
            return "どんな理"
        return "どん"

    try:
        repaired = recover(recognize, image, [donor])
    finally:
        image.close()

    assert len(repaired) == 1
    assert repaired[0]["text"] == "どんな理由が"
    assert repaired[0]["recognizer_retry"] == "vertical-trailing-ink-ocr-v2"
    assert repaired[0]["provenance"]["wide_vertical_trailing_recovery"] is True


def test_v964_late_wide_donor_recovery_is_restricted_to_short_merged_donors() -> None:
    recover = getattr(worker, "_recover_short_wide_vertical_donor_trailing_context", None)
    assert recover is not None
    image = _image()
    donor = _item()
    donor["text"] = "どんな理"
    donor["raw_text"] = "どんな理"
    donor["detector"] = "wide-vertical-text-donor-v1"
    donor["geometry_source"] = "wide-vertical-layout-donor-v1"
    donor["geometry_status"] = "approximate"
    donor["provenance"] = {
        "component_count": 4,
        "component_coverage": 1.0,
        "single_merged_component": False,
        "wide_vertical_text_donor": True,
    }
    calls = 0

    def recognize(_crop: Image.Image) -> str:
        nonlocal calls
        calls += 1
        return "どんな理由が"

    try:
        repaired = recover(recognize, image, [donor])
    finally:
        image.close()

    assert repaired[0]["text"] == donor["text"]
    assert repaired[0]["provenance"] == donor["provenance"]
    assert calls == 0


def test_v965_strict_trailing_recovered_wide_donor_survives_legacy_redundancy_suppress() -> None:
    image = _image()
    donor = _item()
    donor["text"] = "どん"
    donor["raw_text"] = "どん"
    donor["y"] = 1.0 - 102 / 420
    donor["height"] = (102 - 38) / 420
    donor["detector"] = "wide-vertical-text-donor-v1"
    donor["geometry_source"] = "wide-vertical-layout-donor-v1"
    donor["geometry_status"] = "approximate"
    donor["trailing_ink_geometry"] = False
    donor["provenance"] = {
        "component_count": 2,
        "component_coverage": 1.0,
        "single_merged_component": True,
        "detector_bbox_px": [88, 38, 118, 102],
        "cluster_context_donor": True,
        "wide_vertical_text_donor": True,
    }

    def recognize(crop: Image.Image) -> str:
        if crop.height >= 150:
            return "どんな理由が"
        if crop.height >= 115:
            return "どんな理"
        return "どん"

    try:
        repaired = worker._recover_short_wide_vertical_donor_trailing_context(
            recognize, image, [donor]
        )[0]
        assert repaired["text"] == "どんな理由が"
        # Model the legacy peer that currently causes the strict recovered lane
        # to be classified as redundant despite its complete ink-backed stream.
        peer = {
            "text": "別の",
            "raw_text": "別の",
            "orientation": "vertical",
            "x": repaired["x"],
            "y": repaired["y"],
            "width": repaired["width"],
            "height": repaired["height"],
            "source": worker._LAYOUT_LINE_SOURCE,
            "detector": worker._LAYOUT_DETECTOR,
            "provenance": {"component_count": 2, "component_coverage": 1.0},
        }
        output = worker._suppress_redundant_implausible_wide_vertical_donors(
            [repaired, peer]
        )
    finally:
        image.close()

    assert any(region.get("text") == "どんな理由が" for region in output)


def test_v966_late_wide_trailing_rejects_suffix_borrowed_from_adjacent_lane() -> None:
    recover = getattr(worker, "_recover_short_wide_vertical_donor_trailing_context", None)
    assert recover is not None
    image = _image()
    donor = _item()
    donor["text"] = "なけ"
    donor["raw_text"] = "なけ"
    donor["y"] = 1.0 - 102 / 420
    donor["height"] = (102 - 38) / 420
    donor["detector"] = "wide-vertical-text-donor-v1"
    donor["geometry_source"] = "wide-vertical-layout-donor-v1"
    donor["geometry_status"] = "approximate"
    donor["trailing_ink_geometry"] = False
    donor["provenance"] = {
        "component_count": 2,
        "component_coverage": 1.0,
        "single_merged_component": True,
        "detector_bbox_px": [88, 38, 118, 102],
        "cluster_context_donor": True,
        "wide_vertical_text_donor": True,
    }
    peer = {
        "text": "金は払う!!",
        "raw_text": "金は払う!!",
        "orientation": "vertical",
        "x": 106 / 220,
        "y": 1.0 - 180 / 420,
        "width": 28 / 220,
        "height": 130 / 420,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {
            "component_count": 4,
            "component_coverage": 1.0,
            "wide_vertical_text_donor": True,
        },
    }

    def recognize(crop: Image.Image) -> str:
        if crop.height >= 150:
            return "なければ金は"
        if crop.height >= 115:
            return "なければ"
        return "なけ"

    try:
        repaired = recover(recognize, image, [donor, peer])
    finally:
        image.close()

    assert repaired[0]["text"] == "なけ"
    assert repaired[1]["text"] == "金は払う!!"


def test_v966_late_wide_trailing_keeps_nonconflicting_p36_suffix() -> None:
    recover = getattr(worker, "_recover_short_wide_vertical_donor_trailing_context", None)
    assert recover is not None
    image = _image()
    donor = _item()
    donor["text"] = "どん"
    donor["raw_text"] = "どん"
    donor["y"] = 1.0 - 102 / 420
    donor["height"] = (102 - 38) / 420
    donor["detector"] = "wide-vertical-text-donor-v1"
    donor["geometry_source"] = "wide-vertical-layout-donor-v1"
    donor["geometry_status"] = "approximate"
    donor["trailing_ink_geometry"] = False
    donor["provenance"] = {
        "component_count": 2,
        "component_coverage": 1.0,
        "single_merged_component": True,
        "detector_bbox_px": [88, 38, 118, 102],
        "cluster_context_donor": True,
        "wide_vertical_text_donor": True,
    }
    peer = {
        "text": "あろうと!!!",
        "raw_text": "あろうと!!!",
        "orientation": "vertical",
        "x": 106 / 220,
        "y": 1.0 - 180 / 420,
        "width": 28 / 220,
        "height": 130 / 420,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"component_count": 4, "component_coverage": 1.0},
    }

    def recognize(crop: Image.Image) -> str:
        if crop.height >= 150:
            return "どんな理由が"
        if crop.height >= 115:
            return "どんな理"
        return "どん"

    try:
        repaired = recover(recognize, image, [donor, peer])
    finally:
        image.close()

    assert repaired[0]["text"] == "どんな理由が"



def test_v968_wide_promotion_rejects_suffix_borrowed_from_left_peer() -> None:
    conflict = getattr(worker, "_wide_vertical_promoted_chunk_borrows_peer_prefix", None)
    assert conflict is not None
    donor = {
        "text": "",
        "orientation": "vertical",
        "x": 0.576316,
        "y": 0.274167,
        "width": 0.022368,
        "height": 0.053333,
    }
    source = {
        "text": "wide source",
        "orientation": "vertical",
        "x": 0.540053,
        "y": 0.259833,
        "width": 0.093579,
        "height": 0.068667,
    }
    peer = {
        "text": "金は払う！！",
        "raw_text": "金は払う！！",
        "orientation": "vertical",
        "x": 0.540605,
        "y": 0.259833,
        "width": 0.035895,
        "height": 0.068667,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"component_count": 2, "component_coverage": 0.9875},
    }

    assert conflict(donor, "なければ金は", source, [source, peer]) is True


def test_v968_wide_promotion_keeps_nonconflicting_p36_chunk() -> None:
    conflict = getattr(worker, "_wide_vertical_promoted_chunk_borrows_peer_prefix", None)
    assert conflict is not None
    donor = {
        "text": "",
        "orientation": "vertical",
        "x": 0.576316,
        "y": 0.274167,
        "width": 0.022368,
        "height": 0.053333,
    }
    source = {
        "text": "wide source",
        "orientation": "vertical",
        "x": 0.540053,
        "y": 0.259833,
        "width": 0.093579,
        "height": 0.068667,
    }
    peer = {
        "text": "あろうと！！！",
        "raw_text": "あろうと！！！",
        "orientation": "vertical",
        "x": 0.540605,
        "y": 0.259833,
        "width": 0.035895,
        "height": 0.068667,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"component_count": 4, "component_coverage": 1.0},
    }

    assert conflict(donor, "どんな理由が", source, [source, peer]) is False



def test_v969_post_trim_crosslane_guard_keeps_repaired_p12_donor() -> None:
    suppress = getattr(
        worker,
        "_suppress_wide_vertical_promoted_chunks_borrowing_peer_prefix",
        None,
    )
    assert suppress is not None

    donor = {
        "text": "ほらガキだ",
        "raw_text": "ほらガキだ",
        "orientation": "vertical",
        "x": 0.45,
        "y": 0.17,
        "width": 0.034211,
        "height": 0.074167,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "provenance": {
            "wide_vertical_text_donor": True,
            "adjacent_tall_prefix_trim": True,
            "adjacent_tall_prefix_trim_original_text": "ほらガキだおも",
            "adjacent_tall_prefix_trim_suffix": "おも",
        },
    }
    peer = {
        "text": "おもしれえ！！",
        "raw_text": "おもしれえ！！",
        "orientation": "vertical",
        "x": 0.410342,
        "y": 0.123167,
        "width": 0.037211,
        "height": 0.123667,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"tall_merged_ocr_consensus": True},
    }

    repaired = suppress([donor, peer])

    assert [piece["text"] for piece in repaired] == ["ほらガキだ", "おもしれえ！！"]


def test_v969_post_trim_crosslane_guard_rejects_unrepaired_p31_donor() -> None:
    suppress = getattr(
        worker,
        "_suppress_wide_vertical_promoted_chunks_borrowing_peer_prefix",
        None,
    )
    assert suppress is not None

    donor = {
        "text": "なければ金は",
        "raw_text": "なければ金は",
        "orientation": "vertical",
        "x": 0.576316,
        "y": 0.274167,
        "width": 0.022368,
        "height": 0.053333,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "provenance": {
            "wide_vertical_text_donor": True,
            "component_count": 8,
            "component_coverage": 1.5645,
        },
    }
    peer = {
        "text": "金は払う！！",
        "raw_text": "金は払う！！",
        "orientation": "vertical",
        "x": 0.540605,
        "y": 0.259833,
        "width": 0.035895,
        "height": 0.068667,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "provenance": {"wide_vertical_text_donor": True},
    }

    repaired = suppress([donor, peer])

    assert [piece["text"] for piece in repaired] == ["金は払う！！"]


def test_v9610_post_trim_crosslane_guard_preserves_legit_p13_overlap() -> None:
    suppress = getattr(
        worker,
        "_suppress_wide_vertical_promoted_chunks_borrowing_peer_prefix",
        None,
    )
    assert suppress is not None

    donor = {
        "text": "今日は顔に大ケ",
        "raw_text": "今日は顔に大ケ",
        "orientation": "vertical",
        "x": 0.630263,
        "y": 0.870833,
        "width": 0.026316,
        "height": 0.071667,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "provenance": {
            "wide_vertical_text_donor": True,
            "component_count": 7,
            "component_coverage": 1.0714,
        },
    }
    peer = {
        "text": "大ケガまで",
        "raw_text": "大ケガまで",
        "orientation": "vertical",
        "x": 0.593237,
        "y": 0.869833,
        "width": 0.038526,
        "height": 0.0745,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {
            "component_count": 4,
            "component_coverage": 0.954,
        },
    }

    repaired = suppress([donor, peer])

    assert [piece["text"] for piece in repaired] == [
        "今日は顔に大ケ",
        "大ケガまで",
    ]


def test_v9611_page_edge_square_retry_overread_is_suppressed() -> None:
    suppress = getattr(worker, "_suppress_page_edge_narrow_vertical_overreads", None)
    assert suppress is not None

    false_edge = {
        "text": "そして、",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.981579,
        "y": 0.349167,
        "width": 0.018421,
        "height": 0.1175,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "selected_hypothesis_id": "manga-ocr-square-retry",
        "provenance": {
            "component_count": 2,
            "component_coverage": 0.9301,
            "single_merged_component": False,
            "detector_bbox_px": [744.86, 637.8, 760.0, 783.2],
        },
    }
    legit_edge = {
        "text": "しかし、",
        "raw_text": "しかし、",
        "orientation": "vertical",
        "x": 0.9735,
        "y": 0.680667,
        "width": 0.0265,
        "height": 0.080333,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "selected_hypothesis_id": "manga-ocr-xy-context-retry",
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.6596,
            "single_merged_component": False,
            "detector_bbox_px": [739.86, 286.8, 760.0, 383.2],
        },
    }
    p33_legit_extension = {
        "text": "まだ居たの",
        "raw_text": "まだ居たの",
        "orientation": "vertical",
        "x": 0.856395,
        "y": 0.129167,
        "width": 0.026684,
        "height": 0.093333,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "selected_hypothesis_id": "manga-ocr-trailing-ink-ocr-v2",
        "provenance": {
            "component_count": 3,
            "component_coverage": 1.093,
            "single_merged_component": False,
            "detector_bbox_px": [650.86, 932.8, 671.14, 978.2],
        },
    }

    repaired = suppress([false_edge, legit_edge, p33_legit_extension])

    assert [piece["text"] for piece in repaired] == ["しかし、", "まだ居たの"]
