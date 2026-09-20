"""Public synthetic checks; never reads or refers to actual manga images."""
import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw


MODULE_PATH = Path(__file__).resolve().parents[1] / "pudge" / "manga_ocr_worker.py"
_spec = importlib.util.spec_from_file_location("p39syntheticworker", MODULE_PATH)
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)


def _poster_parent(manga_text="..."):
    return [
        {"id": "detector-recognition", "text": "$9,876,543 仮の書きで・", "selected": True},
        {"id": "manga-ocr", "text": manga_text, "selected": False},
    ]


def _poster_parts(manga_text="..."):
    shared = {
        "orientation": "horizontal", "source": "manga-ocr/line-split-v2",
        "selected_hypothesis_id": "detector-recognition",
        "hypotheses": _poster_parent(manga_text), "confidence": 1.0,
    }
    return [
        {**shared, "text": "$9,876,543", "x": .09, "y": .53, "width": .16, "height": .022},
        {**shared, "text": "仮の書き", "x": .10, "y": .513, "width": .11, "height": .014},
        {**shared, "text": "で・", "x": .21, "y": .511, "width": .03, "height": .010},
    ]


def test_poster_split_keeps_numeric_header_but_discards_only_two_unsupported_fragments():
    rows = _poster_parts() + [{"text": "別の文", "orientation": "vertical"}]
    result = worker._suppress_detector_only_microtext_below_numeric_caption(rows)
    assert [row["text"] for row in result] == ["$9,876,543", "別の文"]
    assert [row["text"] for row in rows] == ["$9,876,543", "仮の書き", "で・", "別の文"]


def test_real_japanese_full_crop_corroboration_blocks_suppression():
    rows = _poster_parts("仮の書きで")
    assert worker._suppress_detector_only_microtext_below_numeric_caption(rows) == rows


def test_no_shared_parent_no_suppression():
    rows = _poster_parts()
    rows[2] = {**rows[2], "hypotheses": _poster_parent("別の印刷")}
    assert worker._suppress_detector_only_microtext_below_numeric_caption(rows) == rows


def test_standalone_large_caption_does_not_get_removed():
    rows = _poster_parts()[:2]
    assert worker._suppress_detector_only_microtext_below_numeric_caption(rows) == rows


def _synthetic_page():
    page = Image.new("RGB", (760, 1200), "white")
    d = ImageDraw.Draw(page)
    # Two separate large digit-like blobs followed by one independent glyph.
    d.rectangle((189, 699, 197, 718), fill="black")
    d.rectangle((200, 699, 208, 718), fill="black")
    d.line([(198, 726), (189, 743)], fill="black", width=4)
    d.line([(198, 726), (208, 743)], fill="black", width=4)
    digits = {
        "text": "５６", "orientation": "horizontal", "source": "manga-ocr",
        "geometry_source": "short-fullwidth-digit-ink-v1", "pipeline_fingerprint": {"test": "synthetic"},
        "segments": [
            {"text": "５", "source": "short-fullwidth-digit-ink-v1", "x": 189/760,
             "y": 1-719/1200, "width": 9/760, "height": 20/1200},
            {"text": "６", "source": "short-fullwidth-digit-ink-v1", "x": 200/760,
             "y": 1-719/1200, "width": 9/760, "height": 20/1200},
        ],
    }
    neighbor = {
        "text": "独立した文", "orientation": "vertical", "x": 155/760,
        "y": 1-810/1200, "width": 30/760, "height": 110/1200,
    }
    return page, [digits, neighbor]


class _Model:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = 0

    def __call__(self, crop):
        self.calls += 1
        return next(self.answers)


def test_two_independent_readings_add_a_single_glyph_without_mutating_other_regions():
    page, rows = _synthetic_page()
    try:
        assert worker._isolated_glyph_below_short_number(page, rows[0]) is not None
        model = _Model(["人", "人", "又"])
        result = worker._recover_isolated_glyph_after_short_number(model, page, rows)
        assert [r["text"] for r in result] == ["５６", "独立した文", "人"]
        added = result[-1]
        assert added["segments"][0]["text"] == "人"
        assert added["pipeline_fingerprint"] == {"test": "synthetic"}
        assert added["geometry_status"] == "observed"
        assert rows[0]["text"] == "５６"
        assert model.calls == 3
    finally:
        page.close()


def test_disagreement_never_invents_contextual_characters():
    page, rows = _synthetic_page()
    try:
        model = _Model(["人", "入", "八"])
        assert worker._recover_isolated_glyph_after_short_number(model, page, rows) == rows
        assert model.calls == 3
    finally:
        page.close()


def test_ruby_sized_pixels_without_large_glyph_do_not_trigger_ocr():
    page, rows = _synthetic_page()
    try:
        d = ImageDraw.Draw(page)
        d.rectangle((184, 722, 215, 747), fill="white")
        d.line((198, 726, 201, 731), fill="black", width=1)
        model = _Model(["人", "人", "人"])
        assert worker._recover_isolated_glyph_after_short_number(model, page, rows) == rows
        assert model.calls == 0
    finally:
        page.close()


def test_unrelated_number_without_vertical_neighbor_does_not_trigger_model():
    page, rows = _synthetic_page()
    try:
        model = _Model(["人", "人", "人"])
        assert worker._recover_isolated_glyph_after_short_number(model, page, rows[:1]) == rows[:1]
        assert model.calls == 0
    finally:
        page.close()


def test_existing_glyph_is_not_duplicated():
    page, rows = _synthetic_page()
    try:
        box = worker._isolated_glyph_below_short_number(page, rows[0])
        assert box is not None
        l, t, r, b = box
        existing = {"text": "人", "orientation": "vertical", "x": l/760,
                    "y": 1-b/1200, "width": (r-l)/760, "height": (b-t)/1200}
        model = _Model(["人", "人", "人"])
        assert worker._recover_isolated_glyph_after_short_number(model, page, rows+[existing]) == rows+[existing]
        assert model.calls == 0
    finally:
        page.close()
