"""Upper-balloon two-glyph main text is not replaced by its adjacent ruby."""
from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _anchor(top: float = 47) -> dict[str, object]:
    return {
        "text": "さんぞく", "orientation": "vertical",
        "source": worker._LAYOUT_LINE_SOURCE,
        "x": 93/760, "y": 1-(top+57)/1200,
        "width": 16/760, "height": 57/1200,
    }


def _page(monkeypatch, *, third: bool = False):
    image = Image.new("RGB", (760, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((64, 48, 90, 74), fill="black")
    draw.rectangle((64, 75, 91, 103), fill="black")
    comps = [
        {"x": 64, "y": 48, "width": 27, "height": 27},
        {"x": 64, "y": 75, "width": 28, "height": 29},
        {"x": 75, "y": 105, "width": 6, "height": 7},  # ellipsis is not a third glyph
    ]
    if third:
        comps.append({"x": 65, "y": 105, "width": 27, "height": 27})
    monkeypatch.setattr(worker, "_raw_layout_component_candidates", lambda image: comps)
    return image


def test_upper_short_ruby_proposes_only_main_ink(monkeypatch):
    image = _page(monkeypatch)
    proposals = worker._short_ruby_adjacent_upper_main_candidates(image, [_anchor()])
    assert len(proposals) == 1
    proposal, bands = proposals[0]
    assert proposal["text"] == ""
    assert proposal["provenance"]["main_ink_glyph_bands_px"] == [[48, 75], [75, 104]]
    assert bands == [(48, 75), (75, 104)]


def test_upper_main_requires_two_independent_ocr_votes(monkeypatch):
    image = _page(monkeypatch)
    inputs = [_anchor()]
    results = iter(("山賊", "山賊", "山賊"))
    out = worker._recover_short_ruby_adjacent_upper_main(lambda crop: next(results), image, inputs)
    assert len(out) == len(inputs) + 1
    recovered = out[-1]
    assert recovered["text"] == "山賊"
    assert [part["text"] for part in recovered["segments"]] == ["山", "賊"]
    assert [part["text"] for part in inputs] == ["さんぞく"]


def test_upper_main_rejects_one_ocr_vote(monkeypatch):
    image = _page(monkeypatch)
    results = iter(("山賊", "海賊", ""))
    assert worker._recover_short_ruby_adjacent_upper_main(
        lambda crop: next(results), image, [_anchor()]
    ) == [_anchor()]


def test_upper_main_rejects_longer_main_column(monkeypatch):
    image = _page(monkeypatch, third=True)
    assert not worker._short_ruby_adjacent_upper_main_candidates(image, [_anchor()])


def test_upper_main_rejects_already_represented_ink(monkeypatch):
    image = _page(monkeypatch)
    peer = {"text": "全身", "orientation": "vertical", "x": 63/760,
            "y": 1-106/1200, "width": 30/760, "height": 60/1200}
    assert not worker._short_ruby_adjacent_upper_main_candidates(image, [_anchor(), peer])


def test_upper_main_rejects_lower_anchor(monkeypatch):
    image = _page(monkeypatch)
    assert not worker._short_ruby_adjacent_upper_main_candidates(image, [_anchor(905)])
