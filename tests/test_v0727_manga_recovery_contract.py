from __future__ import annotations

from pudge.manga_ocr_artifact import MANGA_OCR_ARTIFACT_SCHEMA, normalize_region
from tests.t71g06_support import column, horizontal, reader, vertical


def test_artifact_v3_preserves_alignment_diagnostics_and_marks_unlabeled_geometry_unknown() -> None:
    region = normalize_region(
        {
            "text": "海",
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
            "geometry_source": "vision-accurate-aligned-v4+mangaocr-line-v1",
            "line_ocr_text": "海",
            "line_ocr_alignment": {"cost": 0.2, "ops": ["match"]},
            "line_ocr_cost": 0.2,
            "detector_geometry": {"x": 0.09, "y": 0.19, "width": 0.31, "height": 0.41},
            "segments": [{"text": "", "x": 0.1, "y": 0.2, "width": 0.1, "height": 0.1}],
        },
        page_index=4,
        order=2,
    )
    assert MANGA_OCR_ARTIFACT_SCHEMA == "pudge-manga-ocr-v3"
    assert region["geometry_status"] == "unknown"
    assert region["word_geometry"] == "unlabeled_segments"
    assert region["geometry_source"] == "vision-accurate-aligned-v4+mangaocr-line-v1"
    assert region["line_ocr_alignment"] == {"cost": 0.2, "ops": ["match"]}
    assert region["detector_geometry"]["x"] == 0.09


def test_artifact_v3_preserves_bounded_retry_and_selection_evidence() -> None:
    region = normalize_region(
        {
            "text": "理由が",
            "raw_text": "理由",
            "x": 0.1,
            "y": 0.2,
            "width": 0.2,
            "height": 0.4,
            "selected_hypothesis_id": "manga-ocr-context-retry",
            "recognition_selection": "geometry-aware-retry-v1",
            "recognizer_retry": "vertical-y-context-v1",
            "recognition_correction": "mangaocr-small-kana-consensus-v1",
            "geometry_correction": "trailing-ink-v1",
            "recognizer_crop_policy": "tight-line-v2",
            "hypotheses": [
                {"id": "manga-ocr", "text": "理由"},
                {"id": "manga-ocr-context-retry", "text": "理由が", "selected": True},
            ],
        },
        page_index=2,
        order=1,
    )
    assert region["selected_hypothesis_id"] == "manga-ocr-context-retry"
    assert region["recognition_selection"] == "geometry-aware-retry-v1"
    assert region["recognizer_retry"] == "vertical-y-context-v1"
    assert region["recognition_correction"] == "mangaocr-small-kana-consensus-v1"
    assert region["geometry_correction"] == "trailing-ink-v1"
    assert region["recognizer_crop_policy"] == "tight-line-v2"
    assert len(region["hypotheses"]) == 2


def test_artifact_does_not_call_synthetic_grid_observed_geometry() -> None:
    region = normalize_region(
        {
            "text": "小さな",
            "x": 0.1,
            "y": 0.2,
            "width": 0.1,
            "height": 0.3,
            "segments": [
                {"text": "小", "x": 0.1, "y": 0.4, "width": 0.05, "height": 0.05, "source": "ink-grid-v1"},
                {"text": "さ", "x": 0.1, "y": 0.35, "width": 0.05, "height": 0.05, "source": "ink-grid-v1"},
            ],
        },
        page_index=1,
        order=0,
    )
    assert region["geometry_status"] == "synthetic"


# Word hitboxes, as the OCR debug overlay (Shift+D twice: "words") draws them.
HITBOX_PAGE = {
    0: [
        # The recognizer read more than the detector found ink for: "な猫" has no segments.
        column("猫が好きな猫", 0.80, 0.90, segment_text="猫が好き"),
        vertical("魚", 0.60, 0.70, 0.05, 0.10),  # no segments, one word: the region is the word
        vertical("白い犬", 0.50, 0.60, 0.05, 0.25),  # no segments, two words: no invented grid
        horizontal("赤い花", 0.10, 0.10, 0.30, 0.05),  # ... nor a horizontal one
        column("青い鳥", 0.30, 0.90, segment_text="青"),  # a single segment for two words: nothing
        column("空", 0.25, 0.50),  # one segment: not enough to place a word, and the
        # region is not used as the word's box instead
        column("海だ", 0.20, 0.90, source="vision-accurate", status=None),
    ]
}
HITBOX_PARSE = {
    "猫が好きな猫": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0], ["な", 4, 0], ["猫", 1, 0]],
    "魚": [["魚", 5, 0]],
    "白い犬": [["白い", 6, 0], ["犬", 7, 0]],
    "赤い花": [["赤い", 8, 0], ["花", 9, 0]],
    "青い鳥": [["青い", 10, 0], ["鳥", 11, 0]],
    "海だ": [["海", 12, 0], ["だ", 13, 0]],
    "空": [["空", 14, 0]],
}


def test_word_hitboxes_follow_the_ocr_ink_in_order_and_are_never_invented(page) -> None:
    result = reader(
        page,
        r"""
        await __key('D', {shiftKey: true, code: 'KeyD'}); await __tick(10);
        await __key('D', {shiftKey: true, code: 'KeyD'}); await __tick(10);
        return {mode: $('mangaReaderV2').dataset.debugOverlay, button: __text('[data-manga-v2-action="toggle-debug-overlay"]'),
                label: t('manga.ocrDebugWords'), shown: regions().map(r => r.text), boxes: debugBoxes()};
        """,
        regions=HITBOX_PAGE,
        parse=HITBOX_PARSE,
    )
    out = result.value
    assert (out["mode"], out["button"]) == ("words", out["label"])
    boxes = {}
    for box in out["boxes"]:
        boxes.setdefault(out["shown"][box["region"]], []).append(
            (box["word"], box["token"], box["source"], box["status"])
        )
    # Words map onto the ink in reading order; a word the ink does not cover gets no box
    # (the second 猫 is not mapped back onto the first one).
    assert boxes["猫が好きな猫"] == [
        ("猫", 0, "ink-components-v2", "approximate"),
        ("が", 1, "ink-components-v2", "approximate"),
        ("好き", 2, "ink-components-v2", "approximate"),
    ]
    assert boxes["魚"] == [("魚", 0, "region-single-token", "unavailable")]
    assert boxes["海だ"] == [
        ("海", 0, "vision-accurate", "observed"),
        ("だ", 1, "vision-accurate", "observed"),
    ]
    assert set(boxes) == {"猫が好きな猫", "魚", "海だ"}


def test_saved_debug_overlay_is_shown_when_a_volume_opens(page) -> None:
    result = reader(
        page,
        "return {mode: $('mangaReaderV2').dataset.debugOverlay, boxes: debugBoxes().length};",
        regions=HITBOX_PAGE,
        parse=HITBOX_PARSE,
        localStorage={"pudge.manga.debugOverlay": "words"},
    )
    assert result.value["mode"] == "words" and result.value["boxes"] > 0
